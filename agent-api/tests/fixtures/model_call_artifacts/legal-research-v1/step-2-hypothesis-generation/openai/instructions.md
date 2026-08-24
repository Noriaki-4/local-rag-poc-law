# 法的仮説の立案

入力済みの各WorkItemについて、法令検索の対象を選べる暫定的な法的命題を作ります。

## 手順

1. 元の質問と各WorkItemを確認します。
2. 各WorkItemに、その完了判定に必要なHypothesisを1件以上作ります。
3. 命題のうち、法令本文でまだ確認すべき具体的内容を`gaps`へ分けます。

## ルール

- WorkItemを追加、削除、統合しません。
- Hypothesisは未確認で誤り得る暫定回答です。質問の言い換えではなく、主体、行為、法的効果、適用を分ける判定軸を含めます。
- 正確な値や列挙が不明なら推測せず、人数、割合、期間、行為類型等の確認すべき判定軸を示します。
- 「一定の条件」「特定の範囲」「法令上の手続」だけで終わらせません。
- `gaps`には未確認の法的内容を書き、条文名、検索語、検索作業は書きません。
- 根拠条文やArticle IDを推測しません。

<input_contract>
以下は今回の入力項目と意味です。
- `question`: 利用者が回答を求めている元の質問。
- `work_items`: 今回の質問から作成済みのWorkItem。各要素は既知IDと1つの確認事項を持つ。
  - `work_items[].work_item_id`: Programが付与した既知WorkItem ID。
  - `work_items[].question`: このWorkItemで確認する1つの法的事項。
- `non_work_item_requirements`: 質問の明示要求のうち、独立した法的結論を要するWorkItemにしなかった要求。
</input_contract>

{{runtime_input}}

## 出力前の確認

1. 各WorkItemにHypothesisが1件以上あるか確認します。
2. 各Hypothesisが指定したWorkItemだけを検証しているか確認します。
3. Hypothesisが質問の言い換えでなく、検索対象を選べる暫定回答になっているか確認します。
4. `gaps`が条文探しではなく、命題の未確認部分になっているか確認します。
5. 問題があれば修正してから、schemaに従う出力だけを返します。

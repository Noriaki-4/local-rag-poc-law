# 取得本文の評価

## 役割

質問への回答に必要な確認事項について、提示された取得本文を評価し、既存のWorkItem、Hypothesis、
下位規範確認状態へ反映します。この処理では次の検索、Cycle移行、最終回答を決めません。

## 入力

- `work_items`：現在の確認事項と状態です。
- `hypotheses`：本文で支持・反証する暫定命題と未確認事項です。
- `grounding_evidence`：今回、法的判断の根拠に使える取得本文です。
- `required_dependency_work_item_ids`：下位規範の確認状態を必ず返すWorkItemです。

## 手順

1. 各Hypothesisを、それに関係する`grounding_evidence`と照合します。
2. 本文が命題を直接支持または否定する場合だけ、`supported`または`contradicted`へ更新します。
3. 本文だけでは判定できない場合は`unresolved`とし、未確認の法的内容を`gaps`へ残します。
4. Hypothesis更新後に、対応WorkItemを終了できるか評価します。
5. 指定された各WorkItemについて、下位規範確認が不要、追加探索が必要、確認済みのいずれかを判断します。

## ルール

- 検索候補やArticle IDではなく、`grounding_evidence`のEvidence IDだけを根拠に使います。
- `supported`と`contradicted`には直接根拠となるEvidence IDを指定します。
- 回答へ影響するHypothesisが`unresolved`なら、WorkItemを`resolved`にしません。
- 新しいWorkItemやHypothesisは作りません。分解の見直しは次Cycleで行います。
- Tool要求、Cycle移行、最終回答は出力しません。

{{runtime_input}}

## 出力前の確認

1. 本文を提示されていないHypothesisを、学習済み知識だけで判定していないか確認します。
2. `supported`または`contradicted`に、直接根拠となるEvidence IDがあるか確認します。
3. 指定された全WorkItemについて下位規範確認状態を返したか確認します。
4. 次の検索、Cycle移行、最終回答を出力していないか確認します。

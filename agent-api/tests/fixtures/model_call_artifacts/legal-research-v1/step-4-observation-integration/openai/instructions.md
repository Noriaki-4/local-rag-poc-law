# 取得本文の評価

## 役割

質問への回答に必要な確認事項について、提示された取得本文を評価し、既存のWorkItemとHypothesisへ
反映します。この処理では下位規範確認、次の検索、Cycle移行、最終回答を決めません。

## 入力

- `work_items`：今回評価する、現在openの確認事項です。
- `hypotheses`：そのWorkItemに属する暫定命題と未確認事項です。
- `grounding_evidence`：今回、法的判断の根拠に使える取得本文です。
- `contract_feedback`：再試行時だけ提示される、直前出力の契約違反です。
- `previous_observation`：再試行時だけ提示される、修正対象の直前出力です。

## 手順

1. 入力された各Hypothesisの主体、行為、対象、条件を、それに関係する`grounding_evidence`と照合します。
2. 本文が命題を直接支持または否定する場合だけ、`supported`または`contradicted`へ更新します。
3. 本文だけでは判定できない場合は`unresolved`とし、未確認の法的内容を`gaps`へ残します。
4. Hypothesis更新後に、対応WorkItemを終了できるか評価します。

## ルール

- 検索候補やArticle IDではなく、`grounding_evidence`のEvidence IDだけを根拠に使います。
- 同じ制度の本文でも、規律する主体または行為がHypothesisと異なる場合は直接根拠にしません。
- `supported`と`contradicted`には直接根拠となるEvidence IDを指定します。
- 回答へ影響するHypothesisが`unresolved`なら、WorkItemを`resolved`にしません。
- 入力にない完了済みWorkItemとHypothesisは更新しません。
- `contract_feedback`がある場合は`previous_observation`から違反箇所だけを直し、他の判断を維持します。
  `grounding_evidence.evidence_id`を使い、違反文に表示されたArticle IDをコピーしません。
- `contract_feedback`が`needs_action dependency requires open WorkItem IDs`を示す場合、
  そのWorkItemを`resolved`にせず`open`へ戻します。
- 新しいWorkItemやHypothesisは作りません。分解の見直しは次Cycleで行います。
- Tool要求、Cycle移行、最終回答は出力しません。

{{runtime_input}}

## 出力前の確認

1. 本文を提示されていないHypothesisを、学習済み知識だけで判定していないか確認します。
2. 主体、行為、対象、条件が異なる本文を直接根拠にしていないか確認します。
3. `supported`または`contradicted`に、直接根拠となるEvidence IDがあるか確認します。
4. 再試行では`previous_observation`の違反箇所以外を変えていないか確認します。
5. `needs_action`の下位規範確認が残ると指摘されたWorkItemを`resolved`にしていないか確認します。
6. 下位規範確認、次の検索、Cycle移行、最終回答を出力していないか確認します。

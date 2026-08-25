# 法令調査Solver：取得本文の評価

## 目的

質問への回答に必要な確認事項について、提示された取得本文を評価し、既存のWorkItemとHypothesisへ
反映します。この処理では下位規範確認、次の検索、Cycle移行、最終回答を決めません。

## 出力

- `update_work_items`：今回の本文評価により変わるWorkItem
- `update_hypotheses`：今回の本文評価により変わるHypothesis

## 完了条件

- 提示本文を、対応するHypothesisの主体・行為・対象・条件と照合している。
- 判定とEvidence IDが一致し、未確認事項だけが`gaps`に残っている。
- WorkItemを閉じる場合、その質問へ本文から回答できる。

## 入力

- `work_items`：今回評価する、現在openの確認事項です。
- `hypotheses`：そのWorkItemに属する暫定命題と未確認事項です。
- `evidence_hypothesis_candidates`：本文取得前に、各Articleが直接検証できると判断したHypothesisの候補対応です。
  対応候補であり、支持・反証を確定するものではありません。
- `grounding_evidence`：今回、法的判断の根拠に使える取得本文です。
- `contract_feedback`：再試行時だけ提示される、直前出力の契約違反です。
- `previous_observation`：再試行時だけ提示される、修正対象の直前出力です。

## 手順

1. `evidence_hypothesis_candidates`を手掛かりに、各Hypothesisの主体、行為、対象、条件を、
   対応Articleの`grounding_evidence`と照合します。
2. 本文が命題を直接支持または否定する場合だけ、`supported`または`contradicted`へ更新します。
3. 本文で一部だけ確認できた場合は`unresolved`を維持し、確認に使ったEvidence IDを残したうえで、
   本文確認済みの内容を`gaps`から除き、残る未確認事項だけを書きます。
4. 本文がHypothesisに関係しない場合は`unresolved`を維持し、Evidence IDを結び付けません。
5. Hypothesis更新後に、対応WorkItemを終了できるか評価します。
6. WorkItemを`resolved`にする場合は、その結論に使った判定済みHypothesis IDを
   `basis_hypothesis_ids`へ入れます。

## ルール

### EvidenceとHypothesis

- 検索候補やArticle IDではなく、`grounding_evidence`のEvidence IDだけを根拠に使います。
- 同じ制度の本文でも、規律する主体または行為がHypothesisと異なる場合は直接根拠にしません。
- WorkItemの`question`が示す確認対象だけを評価します。範囲を確認するWorkItemへ、例外・成立条件・手続を
  混ぜません。別の確認対象を定める本文は、同じ制度や同じ取得要求に含まれていても直接根拠にしません。
- WorkItemが「主な場合」「例」「種類」等を求める場合、例外等が存在すると述べるだけの上位規定では
  回答済みにしません。提示本文にある具体的な場合を選び、その直接根拠を残します。
- 候補対応があっても、本文がHypothesisを直接支持または否定しなければ判定済みにしません。
- 本文がHypothesisより狭い範囲や異なる内容を定める場合、Hypothesisの文を読み替えて`supported`に
  しません。命題を否定できるなら`contradicted`、結論まで確認できなければ`unresolved`にします。
- `supported`と`contradicted`には直接根拠となるEvidence IDを指定します。
- `unresolved`のEvidence IDは、本文で確認済みの部分を次Cycleへ引き継ぐために使います。
  未確認の結論を支持する根拠として扱いません。

### WorkItemの終了

- 回答へ影響するHypothesisが`unresolved`なら、WorkItemを`resolved`にしません。
- Hypothesisが判定済みでも、それだけでWorkItemを終了しません。WorkItemの`question`が求める条件・例・範囲・手続を、選んだEvidenceから回答できる場合だけ`resolved`にします。
- `resolution`にはそのWorkItemへの回答だけを書き、別WorkItemの条件・例外・手続を付け足しません。
- 同じArticleのParagraph・Itemが複数提示されている場合は全件を評価しますが、`evidence_ids`にはWorkItemへの回答に直接必要なものだけを選びます。関連するという理由だけで全件を選びません。「主な」例を求める質問では、代表例と質問が明示した条件を選びます。
- 以前のHypothesis判定があっても、新しい本文がより具体的な回答や追加条件を示す場合は、判定理由、
  `evidence_ids`、残る`gaps`を新しい本文に合わせて更新します。
- `resolved`の`basis_hypothesis_ids`は空にせず、そのWorkItemに属する判定済みHypothesisだけを指定します。

### 更新範囲と再試行

- 入力にない完了済みWorkItemとHypothesisは更新しません。
- `contract_feedback`がある場合は`previous_observation`から違反箇所だけを直し、他の判断を維持します。
  `grounding_evidence.evidence_id`を使い、違反文に表示されたArticle IDをコピーしません。
- `contract_feedback`が`needs_action dependency requires open WorkItem IDs`を示す場合、
  そのWorkItemを`resolved`にせず`open`へ戻します。
- 新しいWorkItemやHypothesisは作りません。分解の見直しは次Cycleで行います。
- Tool要求、Cycle移行、最終回答は出力しません。
- 同じWorkItem IDまたはHypothesis IDの更新を複数返しません。判断が一つに定まらない場合も、
  そのIDについて一つの更新にまとめます。

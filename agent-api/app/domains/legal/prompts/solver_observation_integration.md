# 法令調査Solver：取得本文の評価

## 目的

提示された法令本文を既存Hypothesisと照合し、Hypothesisの更新差分を返します。

## 出力

- `update_hypotheses[]`：本文評価後のHypothesis差分
- `decision_reason`：今回の更新理由の要約

## 完了条件

- Hypothesisの判定が、直接根拠となる取得本文に基づいている。
- `gaps[]`には、本文で確認できなかった内容だけが残っている。

## 入力

- `work_items[].question`：今回、取得本文から回答する確認事項
- `hypotheses[]`
  - `judgment`：現在の判定
  - `evidence_ids[]`：現在の判定に使ったEvidence ID
  - `gaps[]`：本文でまだ確認すべき内容
- `evidence_hypothesis_candidates[]`：本文取得前の候補対応
  - `article_id`：`grounding_evidence[].metadata.articleId`と対応する
  - `hypothesis_ids[]`：`hypotheses[].hypothesis_id`と対応する。判定結果ではない
  - `assessment_summary`：Hypothesisとの照合前に候補自身の検索抜粋から作成した要約
- `grounding_evidence[]`：今回評価する取得本文
  - `evidence_id`：本文を根拠として参照するID
  - `metadata.sourceContentUnitId`：本文のParagraph・Item等を示すID
- `contract_feedback`、`previous_observation`：再試行時の修正情報

## 入出力の対応

- `update_hypotheses[].evidence_ids[]`には、そのHypothesisの判断に使用した
  `grounding_evidence[].evidence_id`を設定します。
- `metadata.articleId`や`metadata.sourceContentUnitId`は`evidence_ids[]`に設定しません。
- 複数の本文を根拠にする場合は、対応する`evidence_id`をすべて設定します。

## 手順

1. Hypothesisと本文の候補ペアを作ります。
   - `hypothesis_ids[]`にHypothesis IDを含む候補を選びます。
   - 候補の`article_id`と本文の`metadata.articleId`を一致させます。
   - Hypothesisの`evidence_ids[]`が示す既存本文も含めます。
   - 候補がなければ、提示本文を内容から照合します。
2. 各ペアについて、Hypothesisが確認する規律と法的効果、本文が直接定める規律と法的効果を別々に確認します。
3. 両者が同じ規律を扱う場合だけ、`statement`と各`gaps[]`を本文と個別に比較します。
4. Hypothesisごとに`judgment`、`evidence_ids[]`、`gaps[]`を決めます。
   - `statement`と全ての`gaps[]`を直接確認できた場合だけ`supported`にします。
   - 直接否定する本文があれば`contradicted`にします。
   - 一部だけ確認できた場合を含め、それ以外は`unresolved`にします。確認済みEvidenceと未確認事項は残します。

## ルール

- 候補対応だけを理由にHypothesisを判定しません。
- `assessment_summary`と取得本文が示す規律を確認し、Hypothesisと法的効果の対象が異なる場合は直接根拠にしません。
- 命題と本文の主体、行為、対象、条件又は法的効果が異なる場合は、直接根拠にしません。同じ数値や制度名があっても、別の規律の基準は根拠になりません。
- 本文が複数の要件又は例外を列挙している場合は、WorkItemの事実・条件に対応する項目だけを照合します。
  同じArticleにある別の項目でHypothesisを確認済みにしません。
- 本文が命題の一部だけを扱う場合は、命題を読み替えず`unresolved`にします。
- `statement`が支持されても、各`gaps[]`の内容を本文で確認するまでは`unresolved`とし、そのgapを削除しません。
- 本文がgapの内容を下位規範へ委ねているだけなら、具体化する本文を確認するまでそのgapは未確認です。
- 候補Articleに複数の本文がある場合はすべて比較し、直接必要なEvidence IDだけを使います。
- 入力にないHypothesisを更新せず、新規作成もしません。
- WorkItemの完了状態は出力しません。ProgramがHypothesisと下位規範確認の状態から導出します。
- 再試行では`contract_feedback`が示す違反だけを修正します。
- Tool要求、Cycle移行、最終回答は出力しません。
- 同じIDの更新を複数返しません。

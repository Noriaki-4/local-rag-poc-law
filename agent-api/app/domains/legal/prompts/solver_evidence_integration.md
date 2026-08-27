# 法令調査Solver：取得本文の統合

## 目的

取得した法令本文を既存Hypothesisへ反映し、同じ確認事項に関する下位規範の本文確認状態も判断します。

## 出力

- `update_hypotheses[]`：本文評価後のHypothesis差分
- `dependency_decisions[]`：対象WorkItemの下位規範確認状態
- `decision_reason`：今回の判断の要約

## 入力

- `work_items[]`：今回確認する事項
- `hypotheses[]`：現在の命題、判定、確認済みEvidenceと未確認事項
- `evidence_hypothesis_candidates[]`：本文取得前の対応候補であり、判定結果ではない
  - `article_id`：`grounding_evidence[].metadata.articleId`と対応する
- `grounding_evidence[]`：今回評価する取得本文。`evidence_id`は根拠の参照に使う

## 手順

1. WorkItemとHypothesisが確認する規律を特定します。
2. Hypothesisが確認する規律と法的効果を、取得本文が直接定める内容と照合します。
3. 本文で確認できた内容をHypothesisへ反映し、未確認内容を`gaps`へ残します。
4. 同じ確認事項について、起点規範から必要な末端規範まで本文が揃ったか判断します。

## Hypothesisの判定

- `supported`：`statement`と全ての`gaps`を本文で直接確認できた
- `contradicted`：本文が`statement`を直接否定した
- `unresolved`：一部だけ確認できた場合、又は確認できない内容が残る場合

確認済み部分のEvidenceと未確認事項は`unresolved`でも保持します。本文が命題の一部だけを扱う場合は、命題を読み替えず`unresolved`にします。下位規範へ委ねられた具体的内容は、末端本文を確認するまで`gaps`から削除しません。

## 下位規範の状態

- `not_required`：提示本文だけで確認事項が完結する
- `terminal_text_missing`：関係する末端規範の本文が未確認
- `terminal_text_confirmed`：同じ確認事項を定める起点規範から末端規範まで本文を確認済み

## ルール

- 候補対応や同じ語句だけを理由に本文を根拠にしません。
- 同じArticleの別の要件又は例外を、確認対象の根拠にしません。
- `gaps`には未確認事項だけを書き、確認済み内容を繰り返しません。
- 各理由は判断を区別できる短い1文とし、`gaps`や本文の要約を繰り返しません。
- `evidence_ids`と`basis_evidence_ids`には`grounding_evidence[].evidence_id`だけを使います。
- `terminal_text_confirmed`では、起点規範から末端規範までを上位順に示します。
- 入力にないHypothesisを更新せず、新規作成もしません。
- WorkItemの完了状態は出力しません。Tool、Cycle移行、最終回答も出力しません。
- 再試行では`contract_feedback`が示す違反だけを修正します。

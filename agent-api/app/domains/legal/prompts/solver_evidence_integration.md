# 法令調査Solver：取得本文の統合

## 目的

取得した法令本文を既存Hypothesisへ反映し、同じ確認事項に関する下位規範の本文確認状態と、直後に必要なToolを判断します。

## 出力

- `update_hypotheses[]`：本文評価後のHypothesis差分
- `dependency_decisions[]`：対象WorkItemの下位規範確認状態
- `tool_requests[]`：今回の評価から直ちに必要な次のTool。なければ空
- `decision_reason`：今回の判断の要約

## 入力

- `work_item_session`：このWorkItem専属の論理session IDと現在turn
- `work_items[]`：今回確認する事項
- `hypotheses[]`：現在の命題、判定、確認済みEvidenceと未確認事項
- `evidence_hypothesis_candidates[]`：本文取得前の対応候補であり、判定結果ではなく対応先も制限しない
  - `article_id`：`grounding_evidence[].metadata.articleId`と対応する
- `grounding_evidence[]`：直前のToolで取得し、今回評価する本文。`evidence_id`は根拠の参照に使う
- `dependency_decisions[]`：同じ確認事項について前回までに判断した下位規範状態と根拠ID
- `search_candidates[]`と`fetchable_article_ids[]`：本文未取得の既知候補
- `completed_legal_searches[]`と`completed_graph_searches[]`：成功済みの検索範囲
- `cycle_close_required`：`true`なら現在のCycleを閉じるためToolを返さない

## 手順

1. 取得本文を全ての提示Hypothesisと照合し、本文取得前の対応候補が不適切なら別のHypothesisへ反映します。
2. 同じ確認事項について、起点規範から必要な末端規範まで本文が揃ったか判断します。
3. Hypothesisが確認する規律と法的効果について、取得本文が`statement`を直接支持又は否定するか判断します。
4. 2の判断を踏まえ、WorkItemへの回答に必要な未確認内容を`gaps`へ残します。
5. 未確認事項を直ちに進められる場合だけ、次のToolをWorkItemごとに最大1件選びます。

## Hypothesisの判定

- `supported`：本文が`statement`を直接支持した
- `contradicted`：本文が`statement`を直接否定した
- `unresolved`：本文から`statement`を支持も否定もできない

`judgment`は`statement`の判定、`gaps`はWorkItemへの回答に必要な未確認事項です。
したがって、`supported`でも未確認事項を保持できます。本文から`statement`の一部だけ確認できた場合は、
命題を読み替えず`unresolved`にします。下位規範へ委ねられた具体的内容は、末端本文を確認するまで
`gaps`から削除しません。

## 下位規範の状態

- `not_required`：提示本文だけで確認事項が完結する
- `terminal_text_missing`：関係する末端規範の本文が未確認
- `terminal_text_confirmed`：同じ確認事項を定める起点規範から末端規範まで本文を確認済み

## ルール

- このsessionでは`work_item_session.work_item_id`の確認だけを扱います。後続turnでも、現在提示されたHypothesis、Evidence及び検索履歴を正本とします。
- 候補対応や同じ語句だけを理由に本文を根拠にしません。
- 同じArticleの別の要件又は例外を、確認対象の根拠にしません。
- 質問への回答に関係しない参照先の内容を、新しい`gaps`に追加しません。質問された
  条件、範囲又は手続を参照先へ委ねている場合は、その末端本文を確認対象に残します。
- 既存の`gaps`は、対応する内容を本文で確認した場合だけ削除します。
- `gaps`には未確認事項だけを書き、確認済み内容を繰り返しません。
- 各理由は判断を区別できる短い1文とし、`gaps`や本文の要約を繰り返しません。
- `evidence_ids`と`basis_evidence_ids`には`grounding_evidence[].evidence_id`だけを使います。
- `basis_evidence_ids`には今回新たに判断へ使ったEvidenceだけを書きます。既存の
  `dependency_decisions[].basis_evidence_ids`はProgramが保持するため、繰り返しません。
- `update_hypotheses[].evidence_ids`には今回新たに判断へ使ったEvidenceだけを書きます。既存の
  `hypotheses[].evidence_ids`はProgramが保持するため、繰り返しません。
- 過去に評価済みの本文は再表示されません。現在の判定、確認済みEvidence ID及び未確認事項は
  `hypotheses[]`から引き継ぎます。
- `terminal_text_confirmed`では、起点規範から末端規範までを上位順に示します。
- `not_required`又は`terminal_text_confirmed`では、判断に使った`basis_evidence_ids`を1件以上返します。
- `terminal_text_missing`のWorkItemでは、対応するHypothesisの少なくとも1件に、
  未確認の下位規範の具体的内容を`gaps`として残します。
- 既知候補の本文が必要なら`fetch_articles`、関係と起点を説明できるなら`legal_graph_neighbors`、
  Article又は関係が不明なら`legal_search`、省略済み本文が必要なら`load_evidence`を使います。
- 提示済み本文及び成功済みscopeを繰り返しません。
- `cycle_close_required=true`では`tool_requests=[]`にします。
- `tool_requests[]`は、提示された未確認事項を直接進める要求をWorkItemごとに最大1件返します。
- 同じWorkItemに複数のTool要求を返しません。
- 入力にないHypothesisを更新せず、新規作成もしません。
- WorkItemの完了状態は出力しません。Cycle移行と最終回答も出力しません。
- 再試行では`contract_feedback`が示す違反だけを修正します。

# 法令調査Solver：下位規範本文の確認

## 目的

各WorkItemについて、取得本文だけで回答できるか、同じ確認事項を定める別規範の本文がまだ必要かを判断します。

## 入力

- `question`：元の質問
- `required_dependency_kind`：今回判断する依存の種類
- `work_items[]`：判断対象のWorkItem
- `hypotheses[]`：WorkItemに属する確認中の命題と未確認事項
- `grounding_evidence[]`：取得した法令本文とEvidence ID
- `contract_feedback`、`previous_dependency_assessment`：再試行時だけ提示される修正情報

## 出力

- `decision_reason`：判断全体の短い理由
- `dependency_decisions[]`：WorkItemごとの本文確認状態、理由、判断に使ったEvidence ID
- `dependency_decisions[].action_request_id`：この処理では`null`

## 状態

- `not_required`：取得本文だけで確認事項へ回答でき、同じ事項を定める別規範の確認は不要
- `terminal_text_missing`：同じ事項を定める別規範が本文に示されているが、その本文を未確認
- `terminal_text_confirmed`：確認事項を定める起点規範から末端規範までの本文を確認済み

## 手順

1. WorkItemとHypothesisから、確認する行為、条件、範囲又は方法を特定します。
2. 列挙された規定では、設問の事実に対応する項目を特定してから、その項目の本文を評価します。
3. 対応する項目が同じ確認事項を別規範へ明示的に委ねている場合は、その具体的な本文が
   `grounding_evidence[]`にあるか確認します。具体化規定が同じ事項をさらに委ねていれば、同様に末端まで確認します。
4. WorkItemごとに状態を選び、判断に使ったEvidence IDを規範の上位から下位の順で返します。

## ルール

- WorkItemの確認事項と設問の事実に関係する規定だけを追います。単なる参照、別の項目、又は別規範が存在する
  可能性だけでは`terminal_text_missing`にしません。
- 複数の項目が候補になる場合、設問に示された事実と同じ判断要素を使う項目を確認対象に含めます。
  他の項目だけで概要を説明できても、その項目の確認を省略しません。
- 取得本文が結論の一部を示していても、その結論の条件、範囲又は方法を別規範へ明示的に委ねていれば、
  委ねられた本文を確認するまで`terminal_text_missing`です。
- 列挙中の別項目が完結していることを、確認対象の項目が完結した理由にしません。
- 概要や主な類型を問うWorkItemでは、列挙された全項目の細部を追いません。ただし、回答に採用する項目の
  成立条件が別規範に委ねられている場合は、その条件を確認します。
- `terminal_text_confirmed`では、各規範が同じ確認事項へ何を定めるかを理由に示し、起点から末端までの
  `basis_evidence_ids`を含めます。異なるArticleが複数あるだけでは確認済みにしません。
- `not_required`と`terminal_text_missing`でも、判断に使った`grounding_evidence[].evidence_id`だけを
  `basis_evidence_ids`へ入れます。Article IDをEvidence IDとして使いません。
- 再試行では`previous_dependency_assessment`を基に、`contract_feedback`で示された違反だけを直します。

## 完了条件

- 各`work_items[]`に`dependency_decisions[]`が1件ある。
- 状態と理由が、同じWorkItemの確認事項及び提示本文に対応している。
- `basis_evidence_ids`が提示されたEvidence IDと一致している。

## この処理ではしないこと

WorkItem・Hypothesisの更新、次の検索、Cycle移行、回答は出力しません。

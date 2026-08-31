# 法令調査Solver：Hypothesisの見直し

## 目的

提示された反証済みHypothesisを、取得本文に合う現在版へ見直します。

## 出力

- `revise_hypotheses[]`：同じ命題の見立てを直す既存Hypothesis。更新がなければ空配列
- `add_hypotheses[]`：既存命題とは独立して検証する新しい命題。なければ空配列
- `decision_reason`：更新、追加又は維持した理由の要約

## ルール

- 入力の`hypotheses[]`は、現在のCycleの本文評価で`contradicted`になったものだけです。
- 取得本文により既存Hypothesisの見立てを直す場合は、同じ`hypothesis_id`を`revise_hypotheses[]`へ返します。
- 更新後の`statement`、`judgment`、`gaps[]`には現在版だけを書きます。旧版を併記しません。
- `judgment`は提示本文だけで判断します。本文で確認できない内容が残る場合は`unresolved`にします。
- 既存Hypothesisと独立して検証する命題だけを`add_hypotheses[]`へ返します。
- 内容が既存Hypothesisの`statement`又は`gaps[]`に含まれる場合は追加しません。
- 既存Hypothesisの未確認事項を詳しくした内容や、その結論を支える個別要件は、新しいHypothesisにしません。
- 取得本文に別の義務、条件、例外、定義又は参照があるというだけでは追加しません。既存Hypothesisを保ったまま説明できる内容は追加しません。
- 取得本文だけで確認済みの内容や、利用者の具体的事実の確認は追加しません。
- 更新も追加も不要なら両方を空配列にします。
- WorkItemの範囲を周辺の義務や手続へ広げません。WorkItemの言い換え、検索語や検索方針の変更、一般的な可能性だけでは追加しません。
- 新しい命題は、入力にあるWorkItemのIDを1つ指定します。
- `evidence_ids[]`には提案の必要性を直接示す最小限の取得済みEvidence IDだけを指定します。
- 追加する命題は未確認として扱い、確認が必要な具体的事項を`gaps[]`へ置きます。
- 入力にないHypothesisは変更しません。

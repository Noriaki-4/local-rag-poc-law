# 法令調査Solver：Hypothesisの見直し

## 目的

提示された反証済みHypothesisについて、取得本文から別の命題が必要か見直します。

## 出力

- `add_hypotheses[]`：旧命題を置き換える命題又は独立して検証する新しい命題。なければ空配列
- `decision_reason`：追加又は維持した理由の要約

## ルール

- 入力の`hypotheses[]`は、現在のCycleの本文評価で`contradicted`になったものだけです。
- 既存命題の意味を変える場合は、`replaces_hypothesis_id`に旧IDを指定した新しい命題を返します。
- 旧Hypothesisの内容は変更しません。新Hypothesisは未確認として作成され、旧Evidenceを引き継ぎません。
- 既存Hypothesisと独立して検証する命題では、`replaces_hypothesis_id`を`null`にします。
- 既存Hypothesisを保ったまま説明できる内容や言い換えは追加しません。
- WorkItemの範囲を周辺の義務や手続へ広げません。
- 新しい命題は入力にあるWorkItemを1つ指定し、確認が必要な具体的事項を`gaps[]`へ置きます。
- 追加が不要なら`add_hypotheses[]`を空配列にします。

## 出力前の確認

- 命題の意味を変える場合は、置換元IDを返しているか。
- 置換元Hypothesisと同じWorkItemを指定したか。
- 既存Hypothesisの言い換えを追加していないか。
- 指定したHypothesis及びWorkItem IDが入力に存在するか。

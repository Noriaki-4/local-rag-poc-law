## 現在の作業：Research

## 目的

質問が求める法令上の確認事項を整理し、今回実行する探索を決めます。
Tool結果を受け取った後は、その結果に基づいて次の探索を改めて判断します。

## 手順

1. 質問が求める法令上の確認事項を抽出します。
2. 独立して完了判定できる単位でWorkItemを作ります。
3. 各WorkItemに、法令本文で検証できるHypothesisを作ります。
4. `available_tools`から、未検証のHypothesisに対して今回実行するToolを選びます。
5. 元の質問と`add_work_items`を照合し、漏れ、重複、不要なWorkItemがないことを確認します。
6. 判断結果をSolverDecisionとして返します。

## ルール

- 1つのWorkItemでは、1つの確認事項だけを扱います。
- 質問の主文と、「含めて」「あわせて」等で明示された確認事項をすべて照合します。
- 根拠条文は各WorkItemを検証する材料であり、独立したWorkItemにはしません。
  出典、引用、出力形式、詳しさの指定も同様です。
- 質問にない確認事項は追加しません。
- 複数の確認事項を1つにまとめず、同じ確認事項の材料だけが複数ある場合は分割しません。
- 実行上限は今回選ぶToolにだけ適用し、WorkItemやHypothesisを省略する理由にはしません。
  今回探索しないWorkItemもopenのまま保持します。
- 各Hypothesisは、法令本文で独立に検証できる命題にします。
- 「何らかの規定がある」としか述べないHypothesisは作りません。
  質問から分かる主体と確認対象を含む命題にします。
- `gaps`には、Hypothesisを判定するために法令本文で確認すべき未確認事項を書きます。
- 法令名やArticle IDが未確認なら、推測で補いません。

## Researchモードの終了

- 未検証のWorkItemがある場合は、今回実行するToolを選んでcontinueします。
- 初回時点で法令本文による検証が完了している場合だけ、Toolなしで次へ進めます。

## `decision_reason`

- 今回の分解と探索を選んだ理由を短く書きます。WorkItemの件数や名称は繰り返しません。

## IDの関係

- `Hypothesis.work_item_id`には、そのHypothesisが検証するWorkItemを指定します。
- 元の質問から直接作るopen WorkItemの`basis_hypothesis_ids`は空にします。

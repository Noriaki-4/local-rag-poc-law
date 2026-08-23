## 現在の作業：Reviewer Revision

## 目的

Reviewerの指摘を受け取ったSolverとして、取得本文と照合して指摘を受け入れるか退けるかを判断し、
回答を修正するか追加調査へ戻します。このモードでReviewerとして再レビューは行いません。

## 手順

1. 全Reviewer Findingを取得済み本文と照合します。
2. 各Findingを`addressed / disputed`のどちらかで処理します。
3. 回答を修正するか、WorkItemとHypothesisを再度開いて追加調査します。

### ルール

- 全`finding_id`を`review_finding_resolutions`へ1回ずつ返します。
- 指摘を回答修正または追加調査へ反映する場合は`addressed`にします。
- 提示済み本文に基づいて採用しない場合だけ`disputed`にし、理由とEvidence IDを示します。
- 回答表現だけの問題なら回答を修正します。
- 根拠、観点、下位規範確認が不足する場合は、対応WorkItemとHypothesisを`open / unresolved`へ戻します。
- Reviewerが検索方法を提案しても、その方法を自動採用しません。次の行動はSolverが判断します。
- Findingを未処理のまま再度`finalize`しません。
- `finalize_only=true`なら追加Toolを要求せず、Findingを反映した限定回答を返します。

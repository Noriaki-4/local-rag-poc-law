## 現在の作業：Finalization

## 目的

追加調査できない実行上限時に、確認済み事項と未確認事項を区別した限定回答を作ります。
未確認事項を推測で補完しません。

## 手順

1. Caseでresolvedの事項とopenの事項を確認します。
2. resolvedの事項だけを確認済みとして回答します。
3. 確認済みのWorkItemが0件なら、法的結論を断定しません。確認できなかった旨だけを回答します。
4. openの事項をlimitationsと未解決IDへ列挙します。

### ルール

- `finalize_only=true`ではToolRequestと次Cycle開始を返しません。
- WorkItemとHypothesisの状態は変更しません。
- 入力時点でopenの全WorkItem IDと、それらに属するunresolved Hypothesis IDを回答へ含めます。
- open WorkItemがある場合は、対応する未確認内容をlimitationsへ書きます。
- 確認済みのWorkItemが0件なら、`citation_ids=[]`にします。
- Tool失敗、timeout、候補不在を法的根拠の不存在として断定しません。
- 回答は取得済み本文が示す範囲に限定します。

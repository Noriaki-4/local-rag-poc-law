## Finalizationモード

### 実行手順

1. 取得済み本文とCaseの状態を照合します。
2. 確認済み事項と未確認事項を分けます。
3. 追加Toolなしで最終回答を返します。

### ルール

- `finalize_only=true`ではToolRequestと次Cycle開始を返しません。
- 十分な根拠があるWorkItemだけを`resolved`にします。
- 上限到達を理由に、未確認WorkItemを`resolved / dropped`へ変更しません。
- 未確認事項はlimitations、open WorkItem ID、unresolved Hypothesis IDを対応させます。
- Tool失敗、timeout、候補不在を法的根拠の不存在として断定しません。
- 回答は取得済み本文が示す範囲に限定します。

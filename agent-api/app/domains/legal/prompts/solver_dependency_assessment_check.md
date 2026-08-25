## 出力前の確認

1. 指定された全WorkItemを1回ずつ判断したか確認します。
2. `terminal_text_confirmed`では、質問の同じ論点を定める起点規範から末端規範までのEvidence IDを上位順に示し、`reason`で対応を説明したか確認します。
3. 各`basis_evidence_ids`が`grounding_evidence.evidence_id`と完全一致するか確認します。
4. 中間規範がさらに下位へ委ねる場合や具体例を下位規範が定める場合、末端本文まで確認したか確認します。
5. 下位規範確認以外を出力せず、再試行では`contract_feedback`の違反を直したか確認します。

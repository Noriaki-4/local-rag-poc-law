## 出力前の確認

1. 本文を提示されていないHypothesisを、学習済み知識だけで判定していないか確認します。
2. 主体、行為、対象、条件が異なる本文を直接根拠にしていないか確認します。
3. `evidence_hypothesis_candidates`を根拠の確定結果として扱っていないか確認します。
4. 一部を確認した`unresolved`で、確認済み本文のEvidence IDを落とさず、`gaps`を未確認事項だけに更新したか確認します。
5. `supported`または`contradicted`に、直接根拠となるEvidence IDがあるか確認します。
6. 再試行では`previous_observation`の違反箇所以外を変えていないか確認します。
7. `needs_action`の下位規範確認が残ると指摘されたWorkItemを`resolved`にしていないか確認します。
8. 下位規範確認、次の検索、Cycle移行、最終回答を出力していないか確認します。

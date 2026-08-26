## 出力前の確認

1. `required_transition`に対応する出力だけを返したか確認します。
2. `active_deferred_frontiers`の全IDを`deferred_frontier_resolutions`で1回ずつ扱い、引継ぎIDが許可された候補だけか確認します。
3. 次Cycleへの引継ぎと最終回答を混在させていないか確認します。
4. `finalize`では必須Evidenceを全件引用し、条件の結合、但書、除外、限定を回答へ正しく反映したか確認します。
5. 通常完了では未解決IDと`limitations`が空で、上限時だけ対応する未確認事項を残したか確認します。

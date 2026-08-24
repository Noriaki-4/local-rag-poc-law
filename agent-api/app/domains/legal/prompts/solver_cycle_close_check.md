## 出力前の確認

1. 本文評価を反映済みの状態から、完了または次Cycleを選んだか確認します。
2. `active_deferred_frontiers`の全IDを`deferred_frontier_resolutions`で1回ずつ扱ったか確認します。
3. open WorkItem、unresolved Hypothesis、`needs_action`、後続確認が必要なGraph候補がないのに`start_next_cycle`を選んでいないか確認します。
4. 次Cycleへ送るIDが`retainable_evidence`とopen WorkItemのIDだけか確認します。
5. `start_next_cycle`と`finalize`の出力を混在させていないか確認します。

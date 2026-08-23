## 出力前の完了確認

1. 各WorkItemが、単独で回答できる1つの確認事項だけを表すか確認します。
2. 元の質問が求める複数の回答対象を、1つのWorkItemへ書き写していないか確認します。
3. WorkItemの一方の回答対象だけを回答できるなら、そのWorkItemを分割します。
4. 同じ確認事項へ答えるための材料が複数あるだけなら、機械的に分割しません。
5. 主文で尋ねる対象と、追加で求められた対象の両方がWorkItemに残っているか確認します。
6. 「いつ・どの条件で必要か」と「必要になった場合に何を行うか」を、同じ語があるだけで一つにしていないか確認します。
7. WorkItemを元の質問と照合し、質問が求める確認事項を省略していないか確認します。
8. 取得枠やToolRequest上限に合わせて、WorkItemまたはHypothesisを省略していないか確認します。
9. `remaining_fetch_capacity`をWorkItem、Hypothesisまたは`legal_search`の件数上限として使っていないか確認します。
10. 各WorkItemに本文で真偽を判定できるHypothesisがあり、判定に必要だが未確認の情報が`gaps`に具体的に残っているか確認します。
11. 今回探索するopen WorkItemと、そのHypothesisに対応するToolRequestがあるか確認します。今Stepで探索しないWorkItemはopenのまま残します。
12. `decision_reason`が分解と最初の行動を選んだ理由を説明し、総数が`add_work_items`の件数と一致し、すべての確認対象を短く列挙しているか確認します。
13. 一つでも満たさなければResearch処理は未完了です。Decisionを修正してから返し、確認結果の説明文は追加しません。

## 出力前の完了確認

1. 取得本文で確認できた範囲だけを回答しているか確認します。
2. 入力時点でopenの全WorkItemと、それらに属するunresolved Hypothesisを、`limitations`と未解決IDへ対応付けたか確認します。
3. 回答の各法令記述に、そのArticle自身のgrounding Evidenceを引用したか確認します。
4. ToolRequestと次Cycle開始を返していないか確認し、不足があればDecisionを修正してから返します。

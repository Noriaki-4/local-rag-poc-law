## 出力前の確認

1. 確認済みHypothesisが示す範囲を超えて断定していないか確認します。limitationsで未確認とした内容を回答本文で断定していないか確認し、未解決IDと対応させます。
2. 回答中の法令名が、対応する`material_evidence[].title`と一致するか確認します。
3. `citation_ids`が`grounding_evidence_ids`だけを含み、回答の各法令記述に対応しているか確認します。
4. 未処理の`relevant_deferred`を全件扱ったか確認します。
5. ToolRequest、次Cycle開始、状態更新を返していないか確認し、問題があれば修正してから返します。

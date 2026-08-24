## 出力前の完了確認

1. 今回の全Graph候補とLinkを一度ずつ評価したか確認します。
2. Relationの種類と方向を、対象Hypothesisに照らして判断したか確認します。
3. `select`のArticle数が`graph_review_selection_limit`以内か確認します。
4. 取得枠と直接関係する候補があるのに、本文未確認だけを理由に全件`defer`していないか確認します。
5. `graph_review_selection_limit`が1以上なのに「取得枠がない」と判断していないか確認します。
6. 別候補を優先すると判断した場合、その候補を実際に`select`したか確認します。
7. Graph候補を回答根拠として扱っていないか確認します。

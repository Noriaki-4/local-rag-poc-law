## 出力前の完了確認

1. 新しいToolResultを、対応するWorkItem・Hypothesis・DependencyDecisionへ反映したか確認します。
2. `search_navigation`やGraph候補を、本文根拠として使っていないか確認します。
3. `fetchable_article_ids`に未確認事項を直接扱う候補があれば、成功済みの同じ`legal_search`を繰り返さず`fetch_articles`を選んだか確認します。
4. 既存候補では検証できず再検索する場合は、成功済み検索と異なる検索表現と不足事項を示したか確認します。
5. `fetch_articles.arguments.article_ids`が`fetchable_article_ids`の完全一致だけか確認します。`grounding_evidence_ids`、`basis_evidence_ids`、`metadata.articleId`、Paragraph・ItemのEvidence IDを本文取得要求へ入れません。
6. 必要な本文が`material_evidence`に提示済みなら、再取得せずその本文を評価します。
7. `needs_action`のbasis Evidenceを再取得しようとしていないか確認します。委任先が未確認なら、既知候補、Graph、異なる検索表現から次の行動を選びます。
8. 回答へ影響する未確認事項があれば、取得済み候補を踏まえた次の行動を返しているか確認します。
9. 全観点と下位規範を確認済みの場合だけ`finalize`します。不足があればDecisionを修正してから返します。
10. `recent_tool_results`が空で`search_candidates`がある場合、いずれかのgapを直接確認できる候補を残して`legal_search`を先に選んでいないか確認します。
11. `legal_graph_neighbors`で、`semantic_assertion`には`from_subject / to_subject`、それ以外のmodeには`outgoing / incoming`を使っているか確認します。
12. Article、mode、predicate、directionが同じGraph要求を複数に分けず、関係するHypothesis IDを1要求へまとめたか確認します。
13. `fetch_articles`を複数要求に分けていないか確認します。複数Articleは1要求の`article_ids`、対応する全Hypothesisは同じ要求の`hypothesis_ids`へまとめます。

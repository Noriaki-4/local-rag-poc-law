# Agent Framework fixtures

このディレクトリには、現在のAgent Frameworkの境界を決定的に再現するfixtureだけを置きます。

## 残す基準

- 現行コードがfixtureを読み、契約、状態遷移、ID対応又は探索経路を検査する。
- RUNBOOKの隔離診断で再利用する、最小の入力である。
- 同じ不変条件を検査する、より小さいfixtureがない。
- 実モデルを再実行しなくても、重要な不具合の再発を検出できる。

次のものは置きません。

- 過去のモデル出力を保存していることだけを確認する記録。
- 使われなくなったPrompt、Provider輸送形式又はschemaの記録。
- Issue trackerに経緯を残せば足りる、修正前の回答全文や診断結果。

実モデルの診断結果は`eval-results/`へ出力し、Git管理しません。固定指示、入力、schema、実送信内容の
基準成果物は、`../model_call_artifacts/legal-research-v1/`で別に管理します。意味分類の人手正解データは
再作成コストが高いため、この整理対象には含めません。

## 中核fixture

| 境界 | 代表fixture |
|---|---|
| 質問分解 | `tob_overview_initial_research_decomposition_v1.json` |
| 検索後の状態 | `tob_overview_cycle1_after_search_v1.json` |
| Evidence統合とCycle Close | `tob_overview_cycle1_three_articles_before_cycle_close_v1.json` |
| 次Cycleへの再計画 | `tob_overview_cycle2_replanning_v1.json` |
| 連続1ホップGraph探索 | `lr_003_second_hop_integration_v1.json`、`lr_003_second_hop_graph_review_v1.json`、`lr_003_cycle_close_deferred_frontiers_v1.json` |
| 意味関係Graph探索 | `tob_uses_definition_graph_isolated_v1.json` |
| 未取得本文とHypothesis | `lr_024_missing_terminal_text_consistency_v1.json` |
| EvidenceとHypothesisの対応 | `tob_exceptions_observation_misses_scoped_evidence_v1.json` |
| Graph探索方向 | `tob_overview_dependency_action_wrong_reference_direction_v369.json` |
| timeout時の状態維持 | `tob_cycle_close_observation_update_lost_on_timeout_v1.json` |
| 最小Hypothesis診断 | `tob_minimal_legal_hypothesis_v1.json` |

中核以外のfixtureは、上記の残す基準を満たす固有の回帰用です。新規追加時は、対応するテストから直接参照し、
同じ不変条件の既存fixtureで代用できないことを確認します。

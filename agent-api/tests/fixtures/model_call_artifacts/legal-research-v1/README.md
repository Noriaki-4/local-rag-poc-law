# 法令ResearchのModel呼出し成果物

本番のProfileとModel呼出し生成関数から作る、単一責務Stepの比較用成果物です。
本番実行はこのディレクトリを読みません。生成物は手編集しません。

## 構成

| ディレクトリ | 処理 | 主な確認対象 |
|---|---|---|
| `step-1-question-decomposition` | 質問の要求分解 | WorkItemとWorkItem以外の明示要求 |
| `step-2-hypothesis-generation` | 法的仮説の立案 | WorkItem、Hypothesis、gapsの対応 |
| `step-3-search-planning` | 検索要求の作成 | Hypothesis、`available_tools`、ToolRequest契約 |
| `step-4-observation-integration` | 取得本文の評価 | grounding Evidence、既存状態の意味更新 |
| `step-5-dependency-assessment` | 下位規範依存の評価 | 委任等の有無、追加探索の要否、確認済み根拠 |
| `step-6-cycle-close` | Cycle終了判断 | 完了または次Cycle、引継ぎWorkItem・Evidence |

各段階では、現在の基準Providerである`openai`だけをGit管理します。各Stepの意味契約はProvider非依存であり、
`anthropic`と`ollama`が同じ内容を生成することはファイルを複製せずテスト内で検証します。
Provider別成果物が必要な診断では、出力スクリプトで`eval-results/`へ生成できます。

| ファイル | 内容 |
|---|---|
| `instructions.md` | 固定指示と生成された契約用語集 |
| `input.json` | その段階でLLMへ渡す実行時入力 |
| `output_schema.json` | LLMが返す段階固有の出力契約 |
| `normalized_schema.json` | 共通契約へ正規化した後のschema |
| `request.txt` | 固定指示と実行時入力を結合した実送信内容 |
| `manifest.json` | Profile、model、hash、Prompt assetの来歴 |

## 正本

- `available_tools`一覧の意味：`app/agent_framework/context.py`
- Tool共通項目：`app/agent_framework/tool_contracts.py`
- `legal_search`固有の用途・入出力：`app/adapters/tools/legal_search.py`
- 各段階の手順とルール：`app/domains/legal/prompts/`

`pytest tests/test_model_call_artifacts.py`は、本番コードから再生成した成果物をこの基準と比較します。

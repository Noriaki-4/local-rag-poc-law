# Step1 ローカル Agentic RAG / DeepSearch POC 実装計画・データセット概要

> 本ディレクトリにはユーザー要望の原本だけを置く。
> 設計文書、課題管理、サンプル、評価fixtureの索引は[docs/README.md](../README.md)を参照する。

## 目的

本パッケージは、ローカル Docker 環境で以下を検証するための実装計画とデータセット設計である。

- OpenSearch を使った Vector / Hybrid RAG
- Neo4j を使った GraphRAG（Step2 では Neptune Analytics へ置換想定）
- Agentic DeepSearch の段階的ロジック比較
- 法令QAデータセット lawqa_jp を使った法令RAG検証
- 引用必須の回答・評価ログ設計

## Step1 の位置づけ

Step1 は AWS 本番構成の完全再現ではなく、以下をローカルで検証する。

1. RAG / GraphRAG / Agentic RAG の検索ルート設計
2. 添付メタデータ定義の実運用性
3. 引用付き回答の生成可否
4. lawqa_jp による法令RAG評価

## 重要な前提: e-Gov と lawqa_jp の扱い

本POCでは、e-Gov を実行時のRAG検索先にはしない。

```text
e-Gov
  ↓ 事前ダウンロード
対象法令XML / 法令本文
  ↓ 前処理
条・項・号単位のMarkdown / JSON
  ↓ インデックス登録
OpenSearch / Neo4j
  ↓ 実行時検索
Agentic RAG
```

lawqa_jp の問題文・選択肢・正解は RAG 対象にしない。

- 問題文・選択肢: Agent への入力
- 正解・期待参照条文: 評価用 gold
- e-Gov から事前ダウンロードした法令XML / 法令本文: 前処理後に OpenSearch / GraphDB へ登録する RAG 検索対象
- lawqa_jp 付属コンテキスト: Gold Context 評価または期待根拠照合に使用。通常の Retrieved Context 評価では検索対象にしない
- 回答方法マニュアル: 任意の Instruction RAG 対象

この定義を正とし、各詳細資料ではこの定義を参照する。


## Phase 0 で凍結する項目

実装着手前に、以下を固定する。

- embedding_model
- embedding_dimension
- lawqa_jp の評価対象件数と除外条件
- LLMモデル planner / answer / judge
- Hybrid検索方式と重み
- Pattern 3 の `max_total_tool_calls = 8`、`max_retry_rounds = 2`
- lawqa_jp 選択肢ラベルは内部で大文字 `A`〜`D` に正規化

OpenSearch投入サンプルは非ゼロのダミーベクトルを含むが、評価投入時は必ず実ベクトルに置換する。サンプル説明用の `_note` 等は投入前にstripする。

## 想定 Docker コンポーネント

- MinIO: 原本・処理済み成果物・評価データ置き場
- OpenSearch: 法令本文の Vector / Hybrid 検索
- OpenSearch Dashboards: インデックス確認用
- Neo4j: GraphRAG 検証用。Step2 では Neptune Analytics に置換
- Agent API: FastAPI + LangGraph 等
- Agent UI: Streamlit 等
- Eval Runner: pytest / Python script

## 実装配置

本リポジトリでは、Docker 実装をレポジトリ直下に配置する。

- `docker-compose.yml`: ローカル検証環境
- `agent-api/`: FastAPI Agent API
- `agent-ui/`: Streamlit UI
- `eval-runner/`: 評価実行コンテナ
- `RUNBOOK.md`: 起動・投入・評価手順

## 含まれる資料

- `docs/step1_implementation_plan.md`: Step1 実装計画
- `docs/dataset_design.md`: データセット設計
- `docs/agent_logic_patterns.md`: 単純〜最終系までの4パターン
- `docs/evaluation_design.md`: 評価設計・ログ設計
- `docs/step2_transition_plan.md`: Step2 AWS実現イメージ・移行計画
- `docs/id_naming_rules.md`: ID命名規則と参照整合ルール
- `docs/retrieval_config.md`: 埋め込み・チャンク・Hybrid検索設計
- `docs/legal_issue_coverage_retrieval.md`: 法令向け論点被覆型根拠検索の移行設計
- `docs/layered_legal_evidence_retrieval_plan.md`: 法令レイヤー・法的役割別の根拠探索vNext設計・実装計画
- `docs/graph_edge_construction.md`: Graphエッジ構築方式
- `docs/llm_and_cost_config.md`: LLM選定・固定変数・コスト前提
- `docs/legal_rag_project_checklist.md`: 類似法令RAG案件で再利用する設計・評価チェックリスト
- `docs/samples/`: JSONL / YAML / docker-compose サンプル

## Step2 の実現イメージ

Step2 では、Step1 のローカル構成を AWS 上の検証構成へ移す。MinIO は S3、Neo4j は Neptune Analytics、ローカル OpenSearch は Amazon OpenSearch Serverlessのprivate `VECTORSEARCH` collection、Agent API はBedrock AgentCore Runtimeへ置換する。

AWS上の日本語全文検索ではJapanese（kuromoji）AnalysisとICU Analysisを必須とする。現在の日本語index mappingにあるkuromoji tokenizer、品詞除去、語幹化、読み変換、ICU normalizerを維持し、標準Analyzerへの暗黙のfallbackは許可しない。BM25、vector検索、metadata filterに加え、`_analyze`または同等の確認で日本語token分割を検証する。

初期AWS検証では、時間制約のため正規seedと非同期Relation分類の再実行を必須にしない。OpenSearchの検索全件snapshotと、Neo4jの公開買付けmini Graph snapshot・公開済みClassificationRunを、別snapshotの固定成果物として再利用する。検索側とGraph側のsnapshot IDを同一とは仮定せず、各成果物を別々に検証する。正規seedと非同期処理の実行経路は削除せず、後から再実行可能な状態を維持する。ローカルのbge-m3 vectorはAWSへ移送せず、既存の本文・metadataからTitan V2で再生成する。

詳細は `docs/step2_transition_plan.md` を参照。


## 追加設計メモ

- 権限制御の最小対応表は `docs/clearance_policy.md` を参照。
- OpenSearch embedding dimensionはPhase 0でembeddingモデルと同時に確定する。

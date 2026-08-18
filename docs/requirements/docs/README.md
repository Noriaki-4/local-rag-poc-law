# 設計文書ガイド

このディレクトリには、データ投入、検索、Graph、Agent、評価、将来移行に関する設計文書がある。
すべてが同じ世代の現行仕様ではないため、本書では各文書の役割と位置づけを整理する。

## 1. 現行基盤・Agent設計

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [generic_iterative_agent_framework_plan.md](generic_iterative_agent_framework_plan.md) | 反復Cycle、WorkItem、Hypothesis、Evidence、Graph探索状態、Solver、任意Reviewer、Model Profile、Prompt、CaseStore、trace | 新Agent Frameworkの実装ロードマップの正本 |
| [generic_iterative_agent_framework_plan_visual.md](generic_iterative_agent_framework_plan_visual.md) | 正本の構成、探索、Cycle引継ぎ、Graph、保守性、現行3系統からの切替を図と平易な表現で示す | 上記実装計画と対になる人間向けガイド。仕様判断には使わない |
| [llm_directed_legal_retrieval.md](llm_directed_legal_retrieval.md) | LLM主導の検索・本文取得・Graph展開、LLMとプログラムの責務分担、旧Research Cycle | 新基盤への切替完了までの現行経路仕様 |
| [llm_research_case_store_implementation_plan.md](llm_research_case_store_implementation_plan.md) | 旧ResearchCase、Task、Hypothesis、Event、Checkpoint、トランザクション境界 | 廃止済み。新設計・実装の参照資料にしない |
| [agent_logic_patterns.md](agent_logic_patterns.md) | Baseline RAGからFull DeepSearchまでの4パターン比較 | 初期POC・旧経路の比較設計 |

## 2. データ・インデックス・Graph

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [dataset_design.md](dataset_design.md) | lawqa_jp、e-Gov法令、外部ガイド、RAG対象外データ、原本・派生成果物、投入対象 | 「何を投入するか」の中心文書 |
| [graph_edge_construction.md](graph_edge_construction.md) | `REFERENCES`、`IMPLEMENTS`、`APPLIED_BY`、`EXPLAINS`、`MENTIONS`、RelationAssertionの生成と来歴 | Neo4jインデックス構築の中心文書 |
| [retrieval_config.md](retrieval_config.md) | embedding、Article/Paragraphチャンク、OpenSearchドキュメント、mapping、Hybrid検索 | OpenSearch索引の基本設計 |
| [japanese_legal_search_analysis_plan.md](japanese_legal_search_analysis_plan.md) | Kuromoji、N-gram、BM25・Vector・RRF、v2索引、再シード、shadow比較 | 日本語検索改善の提案・実装前レビュー文書 |
| [id_naming_rules.md](id_naming_rules.md) | 法令・条・項・号・附則・枝番・Graph edgeのID、dangling禁止 | OpenSearchとNeo4jで共有するID規約 |
| [clearance_policy.md](clearance_policy.md) | `confidentiality`、`clearanceLevel`、登録時検査、検索フィルタ | データ登録・検索時のアクセス制御規約 |

## 3. 法令検索・根拠選択

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [layered_legal_evidence_retrieval_plan.md](layered_legal_evidence_retrieval_plan.md) | 法律・政令・府省令のレイヤー、法的役割、Graphオントロジー、ガイドレーン、反復探索、予算 | 法令Domainの詳細設計。多くは実装済みだが、Agentオーケストレーションは新計画へ移行中 |
| [legal_issue_coverage_retrieval.md](legal_issue_coverage_retrieval.md) | 質問の論点分解、30候補から16件への選抜、論点被覆、再ランキング、Graph候補の論点継承 | 旧経路の回答コンテキスト選択改善。feature flag付き実装 |
| [legal_rag_project_checklist.md](legal_rag_project_checklist.md) | データ、索引、日本語検索、Graph、ガイド、LLM状態、時間、評価の確認項目 | 横断的なレビュー・品質確認用チェックリスト |

## 4. 評価・モデル・コスト

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [evaluation_design.md](evaluation_design.md) | 正答率、条文到達率、引用、Graph展開、faithfulness、trace、統計評価 | 評価方法の中心文書 |
| [llm_and_cost_config.md](llm_and_cost_config.md) | LLM用途、初期モデル設定、token・latency・cost計測、比較条件 | 初期POCのモデル・コスト前提。現在の用途別Profileより古い内容を含む |

## 5. 全体計画・将来移行

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [step1_implementation_plan.md](step1_implementation_plan.md) | ローカルDocker、MinIO、OpenSearch、Neo4j、Agent API、UI、評価ランナー | プロジェクト開始時のStep1全体計画 |
| [step2_transition_plan.md](step2_transition_plan.md) | MinIOからS3、Neo4jからNeptune、ローカルOpenSearchからAWS、AgentCore/ECS/Lambdaへの対応 | 将来のAWS移行計画 |

## 6. 文書間の関係

```text
プロジェクト全体
├─ Step1全体構成
│  └─ step1_implementation_plan
│
├─ データ・インデックス
│  ├─ dataset_design
│  ├─ id_naming_rules
│  ├─ retrieval_config
│  ├─ japanese_legal_search_analysis_plan
│  └─ graph_edge_construction
│
├─ 法令検索Domain
│  ├─ layered_legal_evidence_retrieval_plan
│  ├─ legal_issue_coverage_retrieval
│  └─ legal_rag_project_checklist
│
├─ Agent
│  ├─ generic_iterative_agent_framework_plan         正本
│  ├─ generic_iterative_agent_framework_plan_visual  人間向け図解
│  ├─ llm_directed_legal_retrieval            現行旧経路
│  └─ llm_research_case_store...              廃止済み
│
├─ 評価・運用
│  ├─ evaluation_design
│  ├─ llm_and_cost_config
│  └─ clearance_policy
│
└─ 将来
   └─ step2_transition_plan
```

## 7. 推奨読順

1. Agent全体像: [generic_iterative_agent_framework_plan_visual.md](generic_iterative_agent_framework_plan_visual.md)
2. Agent実装仕様: [generic_iterative_agent_framework_plan.md](generic_iterative_agent_framework_plan.md)
3. 投入対象: [dataset_design.md](dataset_design.md)
4. Graph構築: [graph_edge_construction.md](graph_edge_construction.md)
5. OpenSearch: [retrieval_config.md](retrieval_config.md)
6. 法令Domain: [layered_legal_evidence_retrieval_plan.md](layered_legal_evidence_retrieval_plan.md)
7. 評価: [evaluation_design.md](evaluation_design.md)
8. 実行・再投入手順: [RUNBOOK.md](../../../RUNBOOK.md)

## 8. 文書を更新するときの注意

- Agentの将来構造と実装順序は `generic_iterative_agent_framework_plan.md` を正とする。
- データソースや投入対象を変える場合は `dataset_design.md` を更新する。
- Neo4jのnode、edge、RelationAssertion、来歴を変える場合は `graph_edge_construction.md` を更新する。
- OpenSearchのチャンク、mapping、embeddingを変える場合は `retrieval_config.md` を更新する。
- 操作手順や再シード条件を変える場合は、リポジトリ直下の `RUNBOOK.md` も更新する。
- 廃止済み文書を新しい設計判断の根拠にせず、必要な仕様は現行の正本文書へ統合する。
- Graphのインデックス仕様と検索時の利用仕様は別の責務だが、対応するedgeが新Agentから利用可能かを双方の文書で確認する。

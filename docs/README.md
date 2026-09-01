# 設計文書ガイド

> 更新日: 2026-08-22

このディレクトリには、データ投入、検索、Graph、Agent、評価、将来移行に関する設計文書がある。
すべてが同じ世代の現行仕様ではないため、本書では各文書の役割と位置づけを整理する。

## 文書の優先順位

文書間で説明が食い違う場合は、次の順で確認する。

1. ユーザー要望の原本は[requirements/README.md](requirements/README.md)
2. Agentの目標構造・実装順・完了条件は
   [generic_iterative_agent_framework_plan.md](generic_iterative_agent_framework_plan.md)
3. 現行Graph schema version 9のseed仕様は
   [graph_edge_construction.md](graph_edge_construction.md)
4. データ原本・e-Gov XML snapshot・投入対象は[dataset_design.md](dataset_design.md)
5. 実行コマンド、現在のindex名、実測結果は[RUNBOOK.md](../RUNBOOK.md)
6. 実装済みかどうかの最終確認はコード、テスト、評価成果物

古い文書に残る`MENTIONS / APPLIED_BY`、物理Relationとしての`IMPLEMENTS / EXCEPTION_TO`、
旧Graph schemaの記述を、schema version 9へ追加する指示として解釈しない。

## 現在の到達点

- 新Agent FrameworkはPhase 1が一部実装、Phase 2はschema version 9のseed、再開可能な
  非同期Relation分類job、評価データ作成まで実装済み。Luna判定JSONLの検証import、全件分類、
  Hypothesis別selector、旧回答経路からの切替は未完了である。
- OpenSearchは`legal-rag-content-ja-v2`（Kuromoji、NFKC、bigram）を既定とする。
- Graph seedは`HAS_CONTENT_UNIT / REFERENCES / EXPLAINS`だけを決定的に作る。
  5つの意味predicateはseed後に`RelationAssertion`として非同期登録する。
- 保存済みe-Gov XMLからOpenSearchとNeo4jを全件再構築した実績があり、代表94件の構造監査は`94/94`である。
  `改正前の`等で明示された旧版Articleを現行Articleへ接続しない修正も反映した。全件再構築時の証跡は
  OpenSearch 16,459 Content Unit、Neo4j 17,254 node / 34,206 edge、うち`REFERENCES` 16,964件である。
  一方、2026-09-01にAWS bootstrap用としてread-only確認した稼働データは、OpenSearchが同じ16,459
  Content Unit、Neo4jが公開買付けmini Graphの124 node / 172 edgeであり、両者のsnapshotは異なる。
- 非同期意味分類の単位は、1本の物理edgeではなく同じ物理方向の有向Articleペアである。現行exportは
  14,454候補、16,964 basis edge、最大5候補の2,891 shardとなる。代表94件のうち構造的に分類可能な
  73 ArticleペアをLuna Worker / Reviewerでブラインド評価した初回結果は、status `73/73`、5 predicate
  `355/365`、候補単位のpredicate完全一致`63/73`だった。ただし差分監査で旧edge単位goldの移行誤りと、
  同じ法的根拠を示す複数spanを一律不正解にする採点不備が判明したため、この値は最終品質値ではない。
  教師データ・採点契約を修正し、差分候補だけを再評価するまでGate 7の全件分類へ進めない。

## 1. 現行基盤・Agent設計

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [generic_iterative_agent_framework_plan.md](generic_iterative_agent_framework_plan.md) | 反復Cycle、WorkItem、Hypothesis、Evidence、Graph探索状態、Solver、任意Reviewer、Model Profile、Prompt、CaseStore、trace | 新Agent Frameworkの実装ロードマップの正本 |
| [generic_iterative_agent_framework_plan_visual.md](generic_iterative_agent_framework_plan_visual.md) | 正本の構成、探索、Cycle引継ぎ、Graph、5つの意味関係と検索方向、保守性、現行3系統からの切替を図と平易な表現で示す | 上記実装計画と対になる人間向けガイド。仕様判断には使わない |
| [agent_contract_semantics.md](agent_contract_semantics.md) | WorkItem、Hypothesis、Evidence、Tool、Cycle等について、型契約・validator・Context投影・Provider Schema・Promptの対応を示す | 契約項目の意味をPromptだけで再定義しないための保守ガイド |
| [agent_post_run_audit.md](agent_post_run_audit.md) | 判断理由の保存、診断Snapshot、読み取り専用の事後監査API、設定と応答の見方 | 新Agent Frameworkの問題分析時だけ使う簡潔な機能・運用ガイド |
| [relation_classification_rollout_checklist.md](relation_classification_rollout_checklist.md) | 非同期Relation分類の契約固定、構造修正、export/import、100件品質ゲート、全件実行、検索接続の順序と停止条件 | 全件意味分類の実行可否を確認するチェックリスト |
| [llm_directed_legal_retrieval.md](llm_directed_legal_retrieval.md) | LLM主導の検索・本文取得・Graph展開、LLMとプログラムの責務分担、旧Research Cycle | `/answer`からは削除済みの旧経路に関する履歴資料 |
| [llm_research_case_store_implementation_plan.md](llm_research_case_store_implementation_plan.md) | 旧ResearchCase、Task、Hypothesis、Event、Checkpoint、トランザクション境界 | 新規ロードマップとしては置換済み。移行完了までは現行実装の背景・対応確認にだけ使う |
| [agent_logic_patterns.md](agent_logic_patterns.md) | Baseline RAGからFull DeepSearchまでの4パターン比較 | 初期POC・旧経路の比較資料。新Frameworkの責務分担や完了条件には使わない |

## 2. データ・インデックス・Graph

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [dataset_design.md](dataset_design.md) | lawqa_jp、再利用可能なe-Gov XML snapshot、第二期Step 1の公開買付け3階層ミニデータセット、外部ガイド、RAG対象外データ、原本・派生成果物、投入対象 | 「何を投入するか」の中心文書。旧Relation名が残る箇所よりGraph構築仕様を優先する |
| [graph_edge_construction.md](graph_edge_construction.md) | schema version 9、共通snapshot、`HAS_CONTENT_UNIT / REFERENCES / EXPLAINS`の決定的seed、非同期RelationAssertionとの境界 | Neo4jとOpenSearchを同じsnapshotから再構築する現行仕様の正本 |
| [legal_structure_and_graph_search_visual.md](legal_structure_and_graph_search_visual.md) | e-Govの条・項・号、OpenSearch文書、Neo4j Node、Article単位の参照・意味関係、現行Graph検索と再計画案を箱と矢印で示す | 人間向けの構造・検索ガイド。再計画案は正本への反映前であり、現行仕様判断には各正本文書を優先する |
| [retrieval_config.md](retrieval_config.md) | embedding、Article/Paragraphチャンク、OpenSearchドキュメント、mapping、Hybrid検索 | OpenSearch索引の基本設計 |
| [japanese_legal_search_analysis_plan.md](japanese_legal_search_analysis_plan.md) | Kuromoji、N-gram、BM25・Vector・RRF、v2索引、再シード、shadow比較 | 当初の実装前レビュー文書。v2索引は既定化済みのため、現行設定と操作はRUNBOOK・コードを優先する |
| [id_naming_rules.md](id_naming_rules.md) | 法令・条・項・号・附則・枝番・Graph edgeのID、dangling禁止 | OpenSearchとNeo4jで共有するID規約 |
| [clearance_policy.md](clearance_policy.md) | `confidentiality`、`clearanceLevel`、登録時検査、検索フィルタ | データ登録・検索時のアクセス制御規約 |

## 3. 法令検索・根拠選択

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [legal_retrieval_issue_tracker.md](legal_retrieval_issue_tracker.md) | 検索仮説、法令検索表現、OpenSearch反復検索、連続1ホップGraph、完了判断、実モデル評価、全件分類再開条件の課題・優先度・完了条件 | 法令検索の現在地と次の作業を管理する課題台帳。設計判断は各正本文書を優先する |
| [layered_legal_evidence_retrieval_plan.md](layered_legal_evidence_retrieval_plan.md) | 法律・政令・府省令のレイヤー、法的役割、旧Graphオントロジー、ガイドレーン、反復探索、予算 | 旧法令検索経路の詳細設計と実装記録。schema version 3〜6や旧Relationの記述は現行Graph仕様に使わない |
| [legal_issue_coverage_retrieval.md](legal_issue_coverage_retrieval.md) | 質問の論点分解、30候補から16件への選抜、論点被覆、再ランキング、Graph候補の論点継承 | 旧経路の回答コンテキスト選択改善。feature flag付き実装 |
| [legal_rag_project_checklist.md](legal_rag_project_checklist.md) | データ、索引、日本語検索、Graph、ガイド、LLM状態、時間、評価の確認項目 | 横断的なレビュー・品質確認用チェックリスト |

## 4. 評価・モデル・コスト

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [evaluation_design.md](evaluation_design.md) | 正答率、条文到達率、引用、Graph展開、faithfulness、trace、統計評価 | QA・検索評価の中心文書。非同期Relation分類の代表100件は下記成果物とRUNBOOKを正とする |
| [llm_and_cost_config.md](llm_and_cost_config.md) | LLM用途、初期モデル設定、token・latency・cost計測、比較条件 | 初期POCのモデル・コスト前提。現在の用途別Profileより古い内容を含む |

代表100件の正解データと監査記録:

- [legal_relation_guidance_100_manifest.json](samples/eval/legal_relation_guidance_100_manifest.json)
- [legal_relation_94_adjudicated_fixture.jsonl](samples/eval/legal_relation_94_adjudicated_fixture.jsonl)
- [legal_relation_94_adjudication_audit.jsonl](samples/eval/legal_relation_94_adjudication_audit.jsonl)
- [guidance_navigation_fixture.jsonl](samples/eval/guidance_navigation_fixture.jsonl)

## 5. 全体計画・将来移行

| 文書 | 内容 | 現在の位置づけ |
|---|---|---|
| [second_phase_development_memo.md](second_phase_development_memo.md) | 自治体職員の利用像、小規模データセット、法令検索表現、仮説別Graph selector、連続1ホップ、e-Gov構造、意味分類再利用、処理別シーケンス・データ構造図をまとめた第二期再計画 | 第二期開発の備忘録。詳細契約の正本ではなく、確定事項は汎用反復型Agent実装計画へ反映する |
| [step1_implementation_plan.md](step1_implementation_plan.md) | ローカルDocker、MinIO、OpenSearch、Neo4j、Agent API、UI、評価ランナー | プロジェクト開始時のStep1全体計画 |
| [step2_transition_plan.md](step2_transition_plan.md) | MinIOからS3、Neo4jからNeptune、ローカルOpenSearchからAWS、AgentCore/ECS/Lambdaへの対応 | 将来のAWS移行計画 |

## 6. 文書間の関係

```text
プロジェクト全体
├─ ユーザー要望
│  └─ requirements/README                     要望の原本
│
├─ Step1全体構成
│  └─ step1_implementation_plan
│
├─ データ・インデックス
│  ├─ dataset_design
│  ├─ id_naming_rules
│  ├─ retrieval_config
│  ├─ japanese_legal_search_analysis_plan
│  ├─ legal_structure_and_graph_search_visual  構造・検索の人間向け図解
│  └─ graph_edge_construction
│
├─ 法令検索Domain
│  ├─ legal_retrieval_issue_tracker            現在の課題・優先度・完了条件
│  ├─ layered_legal_evidence_retrieval_plan    旧経路の詳細設計・実装記録
│  ├─ legal_issue_coverage_retrieval           旧経路のfeature flag実装
│  └─ legal_rag_project_checklist
│
├─ Agent
│  ├─ generic_iterative_agent_framework_plan         正本
│  ├─ generic_iterative_agent_framework_plan_visual  人間向け図解
│  ├─ agent_post_run_audit                           判断理由の事後説明・監査
│  ├─ llm_directed_legal_retrieval            現行旧経路
│  └─ llm_research_case_store...              置換済み・移行確認用
│
├─ 第二期再計画
│  └─ second_phase_development_memo            重要変更の備忘録
│
├─ 評価・運用
│  ├─ evaluation_design
│  ├─ samples/eval/legal_relation_guidance_100_manifest
│  ├─ llm_and_cost_config
│  └─ clearance_policy
│
└─ 将来
   ├─ step2_transition_plan
   ├─ ../infra/aws/BOOTSTRAP-DATA         AWS初期データの暫定運用
   └─ ../infra/aws/ISSUES                 AWS移行の課題管理
```

## 7. 推奨読順

1. ユーザー要望: [requirements/README.md](requirements/README.md)
2. 第二期の変更概要: [second_phase_development_memo.md](second_phase_development_memo.md)
3. 現在の課題と次の作業: [legal_retrieval_issue_tracker.md](legal_retrieval_issue_tracker.md)
4. Agent全体像: [generic_iterative_agent_framework_plan_visual.md](generic_iterative_agent_framework_plan_visual.md)
5. Agent実装仕様: [generic_iterative_agent_framework_plan.md](generic_iterative_agent_framework_plan.md)
6. 投入対象: [dataset_design.md](dataset_design.md)
7. Graph構築: [graph_edge_construction.md](graph_edge_construction.md)
8. 現在の実装・再投入・評価結果: [RUNBOOK.md](../RUNBOOK.md)
9. OpenSearchの基本設計: [retrieval_config.md](retrieval_config.md)
10. 旧法令検索経路を調べる場合だけ:
   [layered_legal_evidence_retrieval_plan.md](layered_legal_evidence_retrieval_plan.md)
11. QA・検索評価: [evaluation_design.md](evaluation_design.md)
12. AWS移行課題: [infra/aws/ISSUES.md](../infra/aws/ISSUES.md)
13. AWS初期データの暫定運用: [infra/aws/BOOTSTRAP-DATA.md](../infra/aws/BOOTSTRAP-DATA.md)

## 8. 文書を更新するときの注意

- Agentの将来構造と実装順序は `generic_iterative_agent_framework_plan.md` を正とする。
- データソースや投入対象を変える場合は `dataset_design.md` を更新する。
- Neo4jのnode、edge、RelationAssertion、来歴を変える場合は `graph_edge_construction.md` を更新する。
- OpenSearchのチャンク、mapping、embeddingを変える場合は `retrieval_config.md` を更新する。
- Relation分類fixture、predicate数、評価結果を変える場合は、代表100件manifest、監査記録、
  `RUNBOOK.md`、関連テストを同時に更新する。
- 操作手順や再シード条件を変える場合は、リポジトリ直下の `RUNBOOK.md` も更新する。
- 置換済み文書を新しい設計判断の根拠にせず、必要な仕様は現行の正本文書へ統合する。
- Graphのインデックス仕様と検索時の利用仕様は別の責務だが、対応するedgeが新Agentから利用可能かを双方の文書で確認する。

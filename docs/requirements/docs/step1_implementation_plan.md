# Step1 実装計画

## 1. ゴール

ローカル Docker 上で、Vector RAG、GraphRAG、Agentic DeepSearch を段階的に比較できる POC を構築する。

## 2. Step1 の対象範囲

### 対象

- MinIO 上の RAG 用ファイル配置
- 前処理済み成果物の手動配置
- OpenSearch への手動インデックス登録
- Neo4j への手動 Graph インポート
- Agent UI からの質問投入
- Agent による検索・回答・引用生成
- 評価ランナーによるスコアリング

### 対象外

- AWS 完全再現
- AgentCore 完全再現
- 厳密なユーザー管理
- 版管理
- 差分更新
- 大量性能検証
- 複数LLM集約の本格運用

## 3. 推奨アーキテクチャ

```text
[Streamlit UI]
    |
[FastAPI Agent API]
    |
    +-- law_search_tool       -> OpenSearch docType=law
    +-- manual_search_tool    -> OpenSearch docType=manual
    +-- graph_search_tool     -> Neo4j
    +-- source_fetch_tool     -> MinIO
    +-- citation_builder      -> documentId / contentUnitId / sourcePage
    +-- eval_logger           -> JSONL
```

## 4. Docker コンポーネント

```text
minio
opensearch
opensearch-dashboards
neo4j
agent-api
agent-ui
eval-runner
```

Composeの骨子は `samples/docker/docker-compose.outline.yml` を参照。OpenSearch / Neo4j / MinIO は永続volumeを前提にする。

## 5. Phase 0: 実装前に確定する事項

Phase 1着手前に、以下を確定する。

1. ID命名規則とdangling edge禁止ルール。詳細は `docs/id_naming_rules.md`。
2. OpenSearch向けメタデータ形式とindex mapping。詳細は `docs/retrieval_config.md`。
3. 埋め込みモデル、チャンク戦略、Hybrid検索重み。詳細は `docs/retrieval_config.md`。
4. Graphエッジ抽出方式。詳細は `docs/graph_edge_construction.md`。
5. LLM/Judgeモデルとコスト計測の固定条件。詳細は `docs/llm_and_cost_config.md`。
6. 評価件数、評価分割、citationHit粒度。詳細は `docs/evaluation_design.md`。

## 6. 実装フェーズ

### Phase 1: データ配置と検索基盤

1. MinIO に `knowledge-root/` を作成
2. lawqa_jp の参照法令一覧から、e-Govで取得可能な法令番号を抽出
3. e-Gov から対象法令XMLを事前ダウンロード
4. 法令XMLを条・項・号単位に Markdown / JSON 化
5. OpenSearch向けドキュメントJSONとmetadataを生成
6. 前処理済み法令本文を MinIO に配置
7. OpenSearch に法令本文を投入
8. Neo4j に Article / Paragraph / REFERENCES / DEFINES / EXCEPTION_TO 等を投入
9. dangling edge検査を実行し、参照先ノードが存在しないedgeを禁止する

### Phase 2: Baseline RAG

1. lawqa_jp 問題文 + 選択肢を入力
2. OpenSearch のみで、事前登録済みのe-Gov由来法令本文を検索
3. LLM が選択肢を判定
4. predictedAnswer と citations を出力
5. goldAnswer と照合

### Phase 3: Rule-based Agentic RAG

1. ルールで検索ルートを決定
2. 条件に応じて GraphRAG を追加
3. 条例マニュアルでは manual → graph → law を実行
4. 引用付き回答を生成

### Phase 4: Controlled Agentic RAG

1. LLM でクエリ分解
2. LLM が検索ルートを提案
3. Orchestrator がバリデーション・補正
4. 根拠不足時のみ最大2回再検索
5. ログに各 round を保存

### Phase 5: Full DeepSearch の一部検証

1. Instruction RAG を追加
2. 回答方法マニュアルを検索して Planner に渡す
3. 失敗診断・局所修復は設計のみ、初期実装では任意

## 7. e-Gov 法令取得の位置づけ

本項はREADMEの「重要な前提」を参照する。各ドキュメントで異なる表現を増やさない。

## 8. 必須制御

Agent に自由に決めさせない項目:

```text
deptCode
allowedGroups
clearanceLevel
publishStatus
isLatest
confidentiality
```

Orchestrator が必ず注入する項目:

```text
publishStatus = published
isLatest = true
docType = law / manual / reasoning_manual
```

## 9. 引用必須ルール

- 法的判断には law evidence が必須
- 業務手順には manual evidence が必須
- manual → law の関係を使った場合、Graph path と law 本文引用を両方出す
- Graph edge だけで法的根拠の代替にしない
- 引用が取れない場合は断定しない

## 10. Step2 への接続

Step1 の成果物は、Step2 の AWS 検証にそのまま移行できる形にする。

```text
MinIO              -> S3
OpenSearch Docker  -> Amazon OpenSearch Service / OpenSearch Serverless
Neo4j              -> Neptune Analytics
FastAPI Agent API  -> AgentCore Runtime または ECS / Lambda
Streamlit UI       -> GenU / 独自UI / Streamlit on ECS
JSONL評価ログ       -> S3 / CloudWatch Logs / DynamoDB
```

Step2 の詳細な実現イメージは `docs/step2_transition_plan.md` に記載する。


補足: `confidentiality` と `clearanceLevel` の対応は `docs/clearance_policy.md` を正とする。

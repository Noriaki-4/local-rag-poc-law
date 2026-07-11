# Step2 AWS 実現イメージ・移行計画

## 1. Step2 の目的

Step2 は、Step1 でローカル Docker 上に構築した Agentic RAG / DeepSearch POC を、AWS 上の構成に移し、以下を検証する段階である。

- OpenSearch を使った Vector / Hybrid RAG
- Neptune Analytics を使った GraphRAG
- Agentic DeepSearch による検索ルート制御
- lawqa_jp を使った法令 RAG / GraphRAG 評価
- 引用必須の回答・評価ログ設計

Step2 は本番運用完成版ではない。AWS 上で主要構成要素を手動中心で組み合わせ、Step1 のロジック・データセット・評価設計が AWS 構成でも成立するかを確認する。

## 2. Step2 の想定構成

```text
[Agent UI]
  GenU / 簡易Web UI / Streamlit on ECS 等
        |
[DeepSearch Agent]
  AgentCore Runtime または ECS / Lambda 上の Agent API
        |
        +-- law_search_tool
        |     -> Amazon OpenSearch Service / OpenSearch Serverless
        |
        +-- graph_search_tool
        |     -> Neptune Analytics
        |
        +-- source_fetch_tool
        |     -> Amazon S3
        |
        +-- citation_builder
        |     -> S3 URI / documentId / contentUnitId / sourcePage
        |
        +-- eval_logger
              -> S3 / CloudWatch Logs / DynamoDB 等
```

## 3. Step1 と Step2 の対応表

| 領域 | Step1 ローカル | Step2 AWS |
|---|---|---|
| 原本・成果物置き場 | MinIO | Amazon S3 |
| Vector / Hybrid RAG | OpenSearch Docker | Amazon OpenSearch Service または OpenSearch Serverless |
| GraphRAG | Neo4j | Neptune Analytics |
| Agent 実行 | FastAPI + LangGraph 等 | AgentCore Runtime、または ECS / Lambda |
| Tool 接続 | Python 関数 / HTTP | AgentCore Gateway + Lambda / API、または独自API |
| UI | Streamlit 等 | GenU、Streamlit on ECS、または独自UI |
| 評価ログ | JSONL | S3 / CloudWatch Logs / DynamoDB |
| 法令本文取得 | e-Gov から事前取得 | 同じく事前取得し S3 に配置 |
| lawqa_jp 入力 | ローカルJSONL | S3上の評価データ、またはEval Runnerから投入 |

## 4. Step2 でのデータ配置

Step2 でも、Step1 と同じ論理ディレクトリを S3 上に配置する。

```text
s3://<bucket>/knowledge-root/
  source-documents/
  derived-artifacts/
    vector-documents/
    graph-artifacts/
  document-registry/
  eval/
```

重要な前提:

- e-Govを実行時RAG検索先にはしない。
- e-Govから対象法令XML / 法令本文を事前取得する。
- 前処理済みの条・項・号単位 Markdown / JSON を S3 に配置する。
- OpenSearch / Neptune Analytics へ登録したものを実行時検索対象にする。
- lawqa_jp の問題文・選択肢は Agent 入力であり、RAG検索対象にしない。
- lawqa_jp の正解・期待参照条文・付属コンテキストは評価用 gold として扱う。

## 5. Step2 の手動運用範囲

今回の Step2 は、完全自動パイプラインではなく、AWS マネジメントコンソールや手動スクリプト中心の検証とする。

### 手動でよいもの

- RAG 用ファイルの S3 アップロード
- e-Gov から取得済み法令本文の S3 配置
- 前処理済み Markdown / metadata JSON の S3 配置
- graph-artifacts の nodes.jsonl / edges.jsonl 配置
- OpenSearch へのインデックス投入
- Neptune Analytics へのグラフデータ投入
- 評価データの投入
- 評価ランナーの手動実行

### 自動化しないもの

- 本格的な文書登録UI
- 承認ワークフロー
- 差分更新
- 版管理
- 大量データの継続同期
- 本番レベルのユーザー管理

## 6. Step2 の実行フロー

### 6.1 lawqa_jp 評価

```text
1. lawqa_jp の問題文 + 選択肢を Eval Runner から Agent に渡す
2. Agent が query decomposition / search route を決定する
3. law_search_tool が OpenSearch 上の e-Gov由来法令本文を検索する
4. 必要に応じて graph_search_tool が Neptune Analytics で REFERENCES / DEFINES / EXCEPTION_TO 等を辿る
5. source_fetch_tool が S3 上の本文・メタデータを取得する
6. Agent が選択肢ごとに supported / contradicted / not_supported を判定する
7. predictedAnswer と citations を出力する
8. goldAnswer / expected references と照合する
```

## 7. Step2 で試す Agent ロジック

Step2 でも、Step1 と同じ4パターンを比較可能にする。

| Pattern | Step2 での意味 |
|---|---|
| Pattern 1: Baseline RAG | OpenSearch のみで lawqa_jp を解く |
| Pattern 2: Rule-based Agentic RAG | 固定ルールで OpenSearch / Neptune を使い分ける |
| Pattern 3: Controlled Agentic RAG | LLM Planner + Orchestrator制御。根拠不足時のみ最大2回再検索 |
| Pattern 4: Full DeepSearch Agent | Instruction RAG、複数回探索、失敗修復を含む最終系。ただしStep2では一部検証でも可 |

Step2 の主目的は、Pattern 1〜3 が AWS 構成でも動くことを確認することである。Pattern 4 は最終像として設計に含めるが、Step2 初期で完全実装する必要はない。

## 8. OpenSearch の役割

OpenSearch は、以下の検索を担当する。

- e-Gov由来法令本文の Vector / Hybrid 検索
- `docType`, `contentDomain`, `publishStatus`, `isLatest`, `deptCode` 等による metadata filter
- 引用生成に必要な `documentId`, `contentUnitId`, `sourceObjectUri`, `sourcePage` の返却

Step2 では Amazon OpenSearch Service と OpenSearch Serverless のどちらでもよいが、Step1 と比較しやすいのは OpenSearch Service である。Serverless を使う場合は、コスト・index設計・metadata filter の検証を別途行う。

## 9. Neptune Analytics の役割

Neptune Analytics は、法令の関係探索を担当する。

主なノード:

```text
Law
Document
Article
Paragraph
Item
Term
Definition
```

主なエッジ:

```text
HAS_ARTICLE
HAS_PARAGRAPH
REFERENCES
DEFINES
USES_TERM
EXCEPTION_TO
APPLIES_TO
```

重要な制約:

- Graph edge だけで最終回答しない。
- Graph は関連条文・根拠条文候補を見つけるために使う。
- 最終回答には、OpenSearch / S3 から取得した本文引用を必ず付ける。

## 10. AgentCore / Agent API の役割

Step2 では、AgentCore を使う場合と、ECS / Lambda 上の独自 Agent API を使う場合がある。

### AgentCore を使う場合

```text
AgentCore Runtime
  - Planner / Agent loop

AgentCore Gateway
  - law_search_tool
  - graph_search_tool
  - source_fetch_tool
```

各 tool は Lambda / ECS / API として実装する。

### 独自 Agent API を使う場合

```text
FastAPI / LangGraph on ECS
  - Planner
  - Orchestrator
  - tool execution
  - citation_builder
  - eval_logger
```

Step2 初期では、AgentCore 完全利用にこだわらず、Step1 の Agent API を ECS / Lambda に載せるだけでもよい。重要なのは、tool interface を将来 AgentCore Gateway に移せる形にしておくことである。

## 11. 必須フィルタと権限制御

Step2 でも、Agent に以下を自由に決めさせない。

```text
deptCode
allowedGroups
clearanceLevel
publishStatus
isLatest
confidentiality
```

Orchestrator / tool 側で必ず強制する。

```text
publishStatus = published
isLatest = true
ユーザーの deptCode
ユーザーの clearanceLevel
ユーザーの allowedGroups
```

Step2 初期ではユーザー属性を固定値でよい。本番化時に Cognito / IAM / GenU のユーザー属性と連携する。

## 12. 評価ログ

Step2 でも Step1 と同じ評価ログ形式を使う。

```json
{
  "runId": "run-001",
  "step": "step2",
  "pattern": "pattern_3_controlled_agentic_rag",
  "dataset": "lawqa_jp",
  "questionId": "q-001",
  "inputType": "multiple_choice_legal_qa",
  "searchPlan": [],
  "toolCalls": [],
  "retrievedContentUnitIds": [],
  "retrievedGraphNodeIds": [],
  "retrievedGraphEdgeIds": [],
  "citations": [],
  "predictedAnswer": "C",
  "goldAnswer": "C",
  "scores": {
    "answerAccuracy": 1,
    "citationHit": 1,
    "retrievalHitAt5": 1,
    "graphExpansionHit": 1
  },
  "latencyMs": 1234
}
```

Step1 と Step2 のログ形式を揃えることで、ローカル構成と AWS 構成の差分を比較しやすくする。

## 13. Step2 で新たに確認すること

Step1 で確認できない、AWS 固有の確認事項は以下である。

- S3 上のディレクトリ設計が運用しやすいか
- OpenSearch の metadata filter が期待どおり効くか
- Neptune Analytics の投入・探索・削除・再投入が現実的か
- AgentCore / Lambda / ECS 経由の tool 呼び出し遅延が許容範囲か
- 評価ログを S3 / CloudWatch / DynamoDB のどこに置くべきか
- 手動アップロード・手動インデックスでPoCが回るか
- Step1 の Pattern 1〜3 の精度差が AWS でも再現するか
- 引用生成に必要な `documentId`, `contentUnitId`, `sourcePage`, `sourceObjectUri` が維持できるか

## 14. Step2 の成功条件

Step2 の成功条件は、以下である。

```text
- lawqa_jp の問題文・選択肢から、S3/OpenSearch/Neptuneに登録済みのe-Gov由来法令本文を検索できる
- predictedAnswer / citations / retrievalHit を評価ログに保存できる
- Graph edge だけでなく、本文引用付きで回答できる
- Pattern 1〜3 の比較結果を出せる
- Step1 で作ったデータセット・メタデータ・評価ログ形式を大きく変えずに使える
```



補足: `confidentiality` と `clearanceLevel` の対応は `docs/clearance_policy.md` を正とする。

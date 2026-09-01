# Step2 AWS 実現イメージ・移行計画

## 1. Step2 の目的

Step2 は、Step1 でローカル Docker 上に構築した Agentic RAG / DeepSearch POC を、AWS 上の構成に移し、以下を検証する段階である。

- OpenSearch を使った Vector / Hybrid RAG
- Neptune Analytics を使った GraphRAG
- Agentic DeepSearch による検索ルート制御
- lawqa_jp を使った法令 RAG / GraphRAG 評価
- 引用必須の回答・評価ログ設計

Step2 は本番運用完成版ではない。AWS 上で主要構成要素を手動中心で組み合わせ、Step1 のロジック・データセット・評価設計が AWS 構成でも成立するかを確認する。

AWS上の機能はGenUの法令検索バックエンドとして提供する。GenUがUI、ユーザー認証、会話操作を
担当し、このリポジトリが法令検索、Graph探索、Agent loop、根拠付き回答を担当する。

## 2. Step2 の想定構成

```text
[GenU]
  UI / 認証 / 会話操作
        |
[Bedrock AgentCore Runtime]
  Legal DeepSearch backend / GenU連携adapter
        |
        +-- law_search_tool
        |     -> Amazon OpenSearch Serverless private collection
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
| Vector / Hybrid RAG | OpenSearch Docker | Amazon OpenSearch Serverlessのprivate `VECTORSEARCH` collection |
| GraphRAG | Neo4j | Neptune AnalyticsのLPGをopenCypherで利用 |
| Embedding | Ollama `bge-m3`（1024次元） | Bedrock Titan Text Embeddings V2（1024次元） |
| Agent 実行 | FastAPI + LangGraph 等 | GenUから呼び出すBedrock AgentCore Runtime |
| Agent用LLM | ローカルLLMを含むprovider切替 | Amazon Bedrock。Gemma 4は移行しない |
| Tool 接続 | Python 関数 / HTTP | Runtime内adapterからAWS serviceを直接呼出し。分離が必要なtoolだけGateway / API化 |
| UI | Streamlit 等 | GenU |
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

### 5.1 初期bootstrapデータ

初期AWS検証は正規seedと非同期Relation分類を再実行せず、ローカルで公開済みの検索全件データと
公開買付けmini Graphを固定成果物として使う。検索とGraphの対象範囲およびsnapshotは意図的に異なる。
これは初期疎通のための暫定対応であり、安全条件と終了条件は
[`infra/aws/BOOTSTRAP-DATA.md`](../infra/aws/BOOTSTRAP-DATA.md)を正本とする。

| 項目 | 初期値 |
|---|---|
| OpenSearch source index | `legal-rag-content-ja-v2` |
| Search snapshot | `snapshot-1e9f9f5c1ac849f7ddffdd7480f80c9f771db7c00efea06a612fc286f8c3d27e` |
| Search data | 20文書（e-Gov 14法令＋6ガイドライン）、16,459 Content Unit |
| 原本manifest | `datasets/lawqa_jp/egov_law_corpus/manifest.json`（14法令） |
| ガイドラインmanifest | `datasets/lawqa_jp/external-guidance/manifest.json`（6文書） |
| Graph snapshot | `snapshot-020185f383d15088b066cfbea48ff5379db05c4e1b48d69d67f209df57f0da46` |
| Graph | 公開買付けmini、124 node、172 edge、3 Document、13 Article、schema version 9 |
| scenario manifest | `datasets/scenarios/public_tender_offer_three_layer_v1/manifest.json` |
| ClassificationRun | `classification-run-public-tender-mini-v1-v23`（published、17候補、24 RelationAssertion） |

Docker volumeやDB物理snapshotを丸ごと移送しない。Search snapshot IDでOpenSearch文書を、Graph snapshot
IDでNeo4j node / edgeを別々に絞り、analysisを含むindex定義、本文・metadata、Graph、公開済みClassificationRunをmanifest
付きJSONLとしてexportし、S3へ配置する。`LEGAL_RELATION_CLASSIFICATION_RUN_ID`が空でも、Neo4j上の指定Runが
`published`でGraph snapshotに属することをread-onlyで検証する。別snapshotの残存node、未公開Run、評価goldは
混ぜない。OpenSearch文書のbge-m3 embeddingは除外し、AWS投入時にTitan Text Embeddings V2で再生成する。

Search snapshotとGraph snapshotが一致することはbootstrapの条件にしない。両者の連携は既存の安定IDで行い、
OpenSearchでは全20文書を検索できる一方、Neptuneで関係探索できるのはmini Graphに含まれる3 Document・
13 Articleの範囲だけである。この非対称性を初期検証の既知制約として扱う。

正規seedと非同期分類は保留するが廃止しない。入力manifest、snapshot・hash監査、再開可能checkpoint、
publish監査を含む既存経路を維持し、初期bootstrapの後に必要になった時点で再開する。

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

Step2 初期からAmazon OpenSearch Serverlessの`VECTORSEARCH` collectionを採用する。collectionは
public公開せず、OpenSearch Serverless管理のVPC endpoint、network policy、data access policy、
AgentCore実行roleを組み合わせて接続する。data plane requestはHTTPSとIAM SigV4を使い、署名serviceは
`aoss`とする。endpoint、region、index名は環境設定とCloudFormation outputから渡し、検索ロジックへ
Serverless固有値を埋め込まない。

日本語BM25検索にはJapanese（kuromoji）AnalysisとICU Analysisを必須とする。現行indexのkuromoji
tokenizer、`kuromoji_baseform`、`cjk_width`、`lowercase`、ICU NFKC normalizerを維持し、未使用の
`kuromoji_part_of_speech`、`kuromoji_stemmer`、`kuromoji_readingform`を要件へ追加しない。collection作成後はindex定義の作成に加えて、日本語の
token分割、BM25、vector、multi-search、metadata filterを実collectionで互換性確認する。Serverlessは
OpenSearch APIの対応範囲が通常のdomainと異なるため、現在利用するAPIを契約テストの対象にする。

## 9. Neptune Analytics の役割

Neptune Analyticsは、法令の関係探索を担当する。Neo4jの現行データモデルに合わせ、RDF/SPARQLによる
ontology graphではなくLPG（Labeled Property Graph）を採用し、openCypherで検索する。RDFをLPGへ
読み替えて投入する経路も初期構成では使わない。

物理node label（schema version 9）:

```text
Document
Article
Paragraph
Item
RelationAssertion
ClassificationRun
ClassificationCheckpoint
```

物理relation type（schema version 9）:

```text
HAS_CONTENT_UNIT
REFERENCES
EXPLAINS
SUBJECT
OBJECT
CLASSIFIED_IN
```

重要な制約:

- Graph edge だけで最終回答しない。
- Graph は関連条文・根拠条文候補を見つけるために使う。
- 最終回答には、OpenSearch / S3 から取得した本文引用を必ず付ける。
- Neo4j Bolt driver、constraint / index文、transaction APIはNeptune Analytics用adapterへ置き換える。

## 9.1 Embedding model

AWSではBedrockの`amazon.titan-embed-text-v2:0`を使用し、出力は1024次元、正規化を有効にする。
現行`bge-m3`と同じ次元数を維持してmapping変更を抑えるが、vector空間には互換性がないため、AWSへ
登録する全document vectorと検索query vectorはTitan V2で再生成する。`bge-m3`の既存vectorを移送しない。
Titan V2は日本語を含む多言語対応だが英語向けに最適化されているため、同じ日本語datasetで`bge-m3`との
retrieval hit、citation hitを比較し、精度低下が許容できない場合はEmbedding modelだけを再選定する。

## 10. GenU連携とAgent実行の役割

GenUからBedrock AgentCore RuntimeとしてLegal Agent backendを呼び出す。GenUからOpenSearch、Graph、
S3を直接呼び出さず、既存のAgent契約とGenU固有のrequest / streaming responseの変換をAgentCore
adapterへ閉じ込める。

対象GenUには外部AgentCore Runtimeを登録する連携口がある。一方、現行FastAPIは同期`/answer`
JSONを返すため、そのままでは互換性がない。Step2ではAgentCore Runtime adapterから既存の
Legal Agent serviceを呼び出し、GenU向けのstreaming eventへ変換する。

### AgentCore Runtime

```text
AgentCore Runtime
  - Planner / Agent loop
  - LLM -> Amazon Bedrock
  - law_search_tool -> OpenSearch
  - graph_search_tool -> Graph service
  - source_fetch_tool -> S3
```

初期構成では既存のtool実装をRuntime内からAWS用adapter経由で呼び出す。別の実行単位や外部serviceへ
分離する必要が生じたtoolだけ、AgentCore Gateway、Lambda、ECSまたはAPI経由へ変更する。

### 補助workload

```text
ECS task等
  - PDF前処理
  - seed
  - eval runner
```

現行の有効な`/answer`、`/search`はrerankerを呼ばないため、旧reranker APIはStep2へ移行しない。
Gemma 4は移行せず、Agentの生成LLMはAgentCore Runtimeから許可されたBedrock modelだけを呼び出す。
Embedding providerは生成LLMと分けて選定し、OpenSearchのvector登録・検索で同じmodelと次元を使う。

ECSは利用者向けAgent APIを公開する基盤にはしない。GenUの画面契約を検索・回答Domainの正本にせず、
AgentCore adapterを外しても、既存のAgent API契約とローカル評価を維持できる構造にする。

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

ユーザー認証はGenUが担当する。Legal Agent backendでは、呼出し主体をAWS側で検証し、GenUから
受け取るユーザー識別子や属性を無条件に信頼しない。PoCで固定属性を使う場合も、固定箇所と
本番時のCognito / IAM属性への対応をGenU連携adapterの設定として明示する。

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
- GenUのrequest、session、streaming表示、citation表示と既存Agent契約を相互変換できるか

## 14. Step2 の成功条件

Step2 の成功条件は、以下である。

```text
- lawqa_jp の問題文・選択肢から、S3/OpenSearch/Neptuneに登録済みのe-Gov由来法令本文を検索できる
- predictedAnswer / citations / retrievalHit を評価ログに保存できる
- Graph edge だけでなく、本文引用付きで回答できる
- Pattern 1〜3 の比較結果を出せる
- Step1 で作ったデータセット・メタデータ・評価ログ形式を大きく変えずに使える
- GenUの認証済み画面からLegal Agent backendを呼び、根拠付き回答を表示できる
```



補足: `confidentiality` と `clearanceLevel` の対応は `docs/clearance_policy.md` を正とする。
